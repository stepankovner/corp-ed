# ACL стенда: вошедшие читают всё, кроме закрытых пространств; public —
# всем, включая анонимов; hr — только группе hr, и в ней страница
# hr:salaries запрещена petr; sales — только группе sales.
*               @ALL        0
*               @user       1
public:*        @ALL        1
hr:*            @user       0
hr:*            @hr         8
hr:salaries     petr        0
sales:*         @user       0
sales:*         @sales      8
